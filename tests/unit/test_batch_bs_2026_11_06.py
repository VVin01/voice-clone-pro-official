"""Batch BS (2026-11-06) — Admin completeness.

Item 1 — plan prices: the pricing seeder covered only the three
MIN_ minute packs, so the other five catalogue plans
(voice_studio_pro / recap_starter / recap_studio_pro /
combined_starter / all_in_one_master) had no PricingPlan row at all:
GET /api/admin/pricing could not list them and POST
/api/admin/pricing/{code} returned 404 — they priced from
PLAN_CATALOGUE alone and no admin surface could edit them. BS
derives the seed from the whole catalogue and fills missing rows
only (idempotent, never overwriting an admin edit), and keeps the
bot buy menu rendering each plan exactly once at the resolved
(DB-wins) price.

Item 2 — model settings: groq_whisper_model was a registered
dynamic setting with no Settings-screen field, and both
transcription paths read the GROQ_WHISPER_MODEL env directly, so
the setting could never take effect. BS wires it end to end: a
shared resolver (dynamic setting > env > default), both readers
routed through it, the payload/GET/validation surface, and a
Settings-screen field riding the Model Chain save. (Gemini TTS
models are a hardcoded rotating LIST in gemini_tts_engine.py —
deliberately no editor; see the batch report.)

Item 3 — webapp broadcast record bug (production 2026-11-06):
POST /api/admin/broadcast built its BroadcastCampaign with
confirmation_token=str(uuid4()) — 36 chars into a String(32)
column — so the record insert failed AFTER the delivery loop had
already sent the broadcast (records rolled back, admin got a 500).
BS mirrors the bot path's nonce expression (uuid4().hex[:12]).
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, Message
from aiogram.types import User as TgUser
from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.config.settings import GROQ_WHISPER_MODEL_DEFAULT, get_groq_whisper_model
from app.models.schema import BotSetting, BroadcastCampaign, PricingPlan, User
from app.services.ledger import get_or_create_user
from app.services.payments import (
    DEFAULT_PRICING_PLANS,
    PLAN_CATALOGUE,
    create_payment_request,
    get_active_pricing_plans,
    update_pricing_plan_price,
)
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
API_PY = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")
PAYMENT_PY = (ROOT / "app/handlers/payment.py").read_text(encoding="utf-8")
TRANSCRIBER_PY = (ROOT / "app/services/transcriber.py").read_text(encoding="utf-8")
WHISPER_SERVICE_PY = (
    ROOT / "app/services/recap_engine/services/whisper_service.py"
).read_text(encoding="utf-8")
ADMIN_HANDLER_PY = (ROOT / "app/handlers/admin.py").read_text(encoding="utf-8")

ADMIN_ID = 999003
ALL_CODES = [e["code"] for e in PLAN_CATALOGUE]
NON_MIN_CODES = [c for c in ALL_CODES if not c.startswith("MIN_")]


def _user(uid: int) -> User:
    return User(id=uid, first_name="Test", username=None,
                account_category="FREE", language_code="my")


async def _all_rows(db_session):
    return (await db_session.execute(
        select(PricingPlan).order_by(PricingPlan.sort_order)
    )).scalars().all()


# ── Item 1: seed covers the whole catalogue, derived from it ──────


def test_bs_seed_list_is_the_full_catalogue():
    assert len(DEFAULT_PRICING_PLANS) == 8
    assert [p["code"] for p in DEFAULT_PRICING_PLANS] == ALL_CODES
    for seed, entry in zip(DEFAULT_PRICING_PLANS, PLAN_CATALOGUE):
        assert seed["minutes"] == entry["minutes"]
        assert seed["price_mmk"] == entry["price_mmk"]
        assert seed["sort_order"] == ALL_CODES.index(entry["code"]) + 1


@pytest.mark.asyncio
async def test_bs_seed_creates_all_eight_rows_on_empty_db(db_session):
    plans = await get_active_pricing_plans(db_session)
    assert [p.code for p in plans] == ALL_CODES
    by_code = {p.code: p for p in plans}
    for entry in PLAN_CATALOGUE:
        row = by_code[entry["code"]]
        assert row.minutes == entry["minutes"]
        assert row.price_mmk == entry["price_mmk"]
        assert row.is_active is True


@pytest.mark.asyncio
async def test_bs_seed_fills_exactly_the_five_missing_rows(db_session):
    # Production shape before BS: only the three MIN_ rows exist.
    for entry in PLAN_CATALOGUE:
        if entry["code"].startswith("MIN_"):
            db_session.add(PricingPlan(
                code=entry["code"], minutes=entry["minutes"],
                price_mmk=entry["price_mmk"], is_active=True,
                sort_order=ALL_CODES.index(entry["code"]) + 1,
            ))
    await db_session.flush()
    assert len(await _all_rows(db_session)) == 3

    plans = await get_active_pricing_plans(db_session)
    rows = await _all_rows(db_session)
    assert len(rows) == 8
    assert [p.code for p in plans] == ALL_CODES
    filled = {r.code for r in rows} - {"MIN_10", "MIN_30", "MIN_60"}
    assert filled == set(NON_MIN_CODES)
    for entry in PLAN_CATALOGUE:
        row = next(r for r in rows if r.code == entry["code"])
        assert row.price_mmk == entry["price_mmk"]


@pytest.mark.asyncio
async def test_bs_reseed_idempotent_and_admin_edit_survives(db_session):
    await get_active_pricing_plans(db_session)
    ok, _plan, _msg = await update_pricing_plan_price(
        db_session, "recap_starter", 21000, admin_id=ADMIN_ID
    )
    assert ok is True
    await get_active_pricing_plans(db_session)
    await get_active_pricing_plans(db_session)
    rows = await _all_rows(db_session)
    assert len(rows) == 8
    by_code = {r.code: r for r in rows}
    assert by_code["recap_starter"].price_mmk == 21000  # edit is sacred
    assert by_code["MIN_10"].price_mmk == 8000          # others untouched


@pytest.mark.asyncio
async def test_bs_admin_pricing_lists_eight_and_edits_new_code(db_session):
    admin = _user(ADMIN_ID)
    res = await api_mod.get_admin_pricing(admin, db_session)
    assert res["count"] == 8
    assert [p["code"] for p in res["plans"]] == ALL_CODES
    recap = next(p for p in res["plans"] if p["code"] == "recap_starter")
    assert recap["price_mmk"] == 20000
    assert recap["category"] == "recap"
    assert recap["recap_credits"] == 5

    # Before BS this POST returned 404 — no row existed to edit.
    out = await api_mod.update_admin_pricing(
        "recap_starter",
        api_mod.AdminPricingUpdateRequest(price_mmk=22000),
        admin, db_session,
    )
    assert out["success"] is True and out["price_mmk"] == 22000
    res2 = await api_mod.get_admin_pricing(admin, db_session)
    recap2 = next(p for p in res2["plans"] if p["code"] == "recap_starter")
    assert recap2["price_mmk"] == 22000


@pytest.mark.asyncio
async def test_bs_submit_overlay_price_for_recap_starter_unchanged(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=700331)
    # No PricingPlan row exists yet: the catalogue fallback charges.
    before = await create_payment_request(
        session=db_session, user_id=user.id,
        package_code="recap_starter", receipt_file_id="slip-before",
    )
    assert before.amount_mmk == 20000
    assert before.minutes == 0

    # Seeding creates the row; the DB-wins overlay must now resolve
    # to exactly the same price/minutes as the catalogue did.
    await get_active_pricing_plans(db_session)
    after = await create_payment_request(
        session=db_session, user_id=user.id,
        package_code="recap_starter", receipt_file_id="slip-after",
    )
    assert after.amount_mmk == before.amount_mmk == 20000
    assert after.minutes == before.minutes == 0


# ── Item 1 knock-on: the bot buy menu stays one button per plan ───


class _FakeFSM:
    async def clear(self):
        return None


@pytest.mark.asyncio
async def test_bs_buy_menu_renders_each_plan_once(db_session, monkeypatch):
    from app.handlers.payment import handle_buy_minutes_menu

    user, _ = await get_or_create_user(db_session, telegram_id=700332)
    user.language_code = "en"
    await db_session.commit()

    @asynccontextmanager
    async def mock_session_ctx():
        yield db_session

    monkeypatch.setattr(
        "app.handlers.payment.AsyncSessionLocal", lambda: mock_session_ctx()
    )
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=700332, is_bot=False, first_name="Buyer")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    await handle_buy_minutes_menu(cb, _FakeFSM())

    kb = cb.message.edit_text.call_args[1]["reply_markup"]
    buy_cbs = [
        btn.callback_data
        for row in kb.inline_keyboard for btn in row
        if btn.callback_data and btn.callback_data.startswith("buy_pkg:")
    ]
    assert len(buy_cbs) == 8
    assert sorted(c.split(":", 1)[1] for c in buy_cbs) == sorted(ALL_CODES)
    # Prices rendered are the resolved (catalogue == seeded DB) ones.
    texts = [
        btn.text for row in kb.inline_keyboard for btn in row
        if btn.callback_data and btn.callback_data.startswith("buy_pkg:")
    ]
    for entry in PLAN_CATALOGUE:
        mine = [t for t in texts if f"{entry['price_mmk']:,}" in t]
        assert mine, entry["code"]


def test_bs_buy_menu_source_keeps_branch_structure():
    # The DB-row loop renders only the minute packs in _db_codes;
    # the R40 Voice Pro branch condition is byte-identical.
    assert '_db_codes = {p.code for p in plans if p.code.startswith("MIN_")}' in PAYMENT_PY
    assert 'entry["category"] == "voice" and entry["code"] not in _db_codes' in PAYMENT_PY
    assert "def _menu_price(entry: dict) -> int:" in PAYMENT_PY


# ── Item 2: Groq Whisper resolver precedence ──────────────────────


def test_bs_groq_resolver_precedence(monkeypatch):
    # Dynamic setting (in-memory attribute, post-overlay) beats env.
    monkeypatch.setattr(settings, "groq_whisper_model", "whisper-custom-9")
    monkeypatch.setenv("GROQ_WHISPER_MODEL", "whisper-env-1")
    assert get_groq_whisper_model() == "whisper-custom-9"

    # No dynamic value: the env wins.
    monkeypatch.setattr(settings, "groq_whisper_model", "")
    assert get_groq_whisper_model() == "whisper-env-1"

    # Nothing set anywhere: the historical default — byte-identical
    # to what both pre-BS read sites produced.
    monkeypatch.delenv("GROQ_WHISPER_MODEL", raising=False)
    assert get_groq_whisper_model() == "whisper-large-v3-turbo"
    assert GROQ_WHISPER_MODEL_DEFAULT == "whisper-large-v3-turbo"


def test_bs_transcribers_route_through_resolver():
    assert "get_groq_whisper_model()" in TRANSCRIBER_PY
    assert 'os.getenv("GROQ_WHISPER_MODEL"' not in TRANSCRIBER_PY
    assert "get_groq_whisper_model()" in WHISPER_SERVICE_PY
    # The standalone-load defensive fallback keeps the env read.
    assert 'os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")' in WHISPER_SERVICE_PY


# ── Item 2: settings surface round-trip ───────────────────────────


@pytest.mark.asyncio
async def test_bs_groq_settings_round_trip(db_session):
    admin = _user(ADMIN_ID)
    saved = getattr(settings, "groq_whisper_model", "")
    try:
        payload = api_mod.AdminSettingsPayload(groq_whisper_model="whisper-large-v3")
        res = await api_mod.update_admin_settings(payload, admin, db_session)
        assert res["updated"]["groq_whisper_model"] == "whisper-large-v3"

        row = (await db_session.execute(
            select(BotSetting).where(BotSetting.key == "groq_whisper_model")
        )).scalar_one()
        assert row.value == "whisper-large-v3"
        assert settings.groq_whisper_model == "whisper-large-v3"

        out = await api_mod.get_admin_settings(admin, db_session)
        assert out["groq_whisper_model"] == "whisper-large-v3"
    finally:
        settings.groq_whisper_model = saved


@pytest.mark.asyncio
async def test_bs_groq_get_prefills_effective_default(db_session, monkeypatch):
    # Nothing set anywhere -> the field shows today's effective model.
    monkeypatch.setattr(settings, "groq_whisper_model", "")
    monkeypatch.delenv("GROQ_WHISPER_MODEL", raising=False)
    out = await api_mod.get_admin_settings(_user(ADMIN_ID), db_session)
    assert out["groq_whisper_model"] == "whisper-large-v3-turbo"


def test_bs_groq_validation_strips_and_caps():
    out = api_mod._validate_admin_settings_updates(
        {"groq_whisper_model": "  whisper-large-v3  "}
    )
    assert out["groq_whisper_model"] == "whisper-large-v3"
    # Empty is a real choice (falls back to env / default).
    out = api_mod._validate_admin_settings_updates({"groq_whisper_model": "   "})
    assert out["groq_whisper_model"] == ""
    with pytest.raises(HTTPException) as exc:
        api_mod._validate_admin_settings_updates(
            {"groq_whisper_model": "x" * 201}
        )
    assert exc.value.status_code == 400


def test_bs_groq_ui_and_i18n_wiring():
    assert 'id="adm-groq-whisper-model"' in INDEX_HTML
    assert 'data-i18n="adm_groq_whisper_label"' in INDEX_HTML
    assert 'data-i18n="adm_groq_whisper_note"' in INDEX_HTML
    # Load wiring: GET value lands in the input (Batch BY: through
    # the admin-settings dirty guard, _admPaintValue).
    assert 'document.getElementById("adm-groq-whisper-model")' in APP_JS
    assert "_admPaintValue(elGroqWhisper, s.groq_whisper_model" in APP_JS
    # Save wiring: the field rides the Model Chain save payload.
    assert "groq_whisper_model:" in APP_JS
    # i18n labels exist in BOTH languages (no hardcoded UI text).
    for key in ("adm_groq_whisper_label", "adm_groq_whisper_note"):
        assert I18N_JS.count(f"{key}:") >= 2, key
    # API surface carries the key end to end.
    assert "groq_whisper_model: Optional[str] = None" in API_PY
    assert '"groq_whisper_model": get_groq_whisper_model()' in API_PY


# ── Item 3: webapp broadcast token fits String(32) ────────────────


def test_bs_broadcast_token_generation_matches_bot_path():
    # The bot path's nonce expression (the mirrored source).
    assert "token = uuid.uuid4().hex[:12]" in ADMIN_HANDLER_PY
    # The webapp campaign insert uses the same expression now, and
    # the 36-char str(uuid4()) form is gone from it.
    assert "confirmation_token=uuid.uuid4().hex[:12]," in API_PY
    assert "confirmation_token=str(uuid.uuid4())," not in API_PY
    token = uuid.uuid4().hex[:12]
    assert len(token) <= 32


def test_bs_campaign_token_column_is_string_32():
    col = BroadcastCampaign.__table__.c.confirmation_token
    assert col.type.length == 32
    campaign = BroadcastCampaign(
        admin_id=ADMIN_ID,
        confirmation_token=uuid.uuid4().hex[:12],
        message_text="check",
    )
    assert len(campaign.confirmation_token) <= col.type.length


@pytest.mark.asyncio
async def test_bs_webapp_broadcast_persists_fitting_token(db_session, monkeypatch):
    """The production bug end to end: delivery succeeds, then the
    campaign record insert must also succeed and carry a token that
    fits String(32) (on Postgres the 36-char form rolled the whole
    record back with StringDataRightTruncationError)."""
    monkeypatch.setattr(settings, "bot_token", "x")
    u1, _ = await get_or_create_user(db_session, telegram_id=12345678)
    await db_session.commit()

    class _FakeBot:
        def __init__(self, *a, **k):
            self.session = SimpleNamespace(close=AsyncMock())

        async def send_message(self, chat_id, text, **kwargs):
            return None

    with patch("aiogram.Bot", _FakeBot):
        resp = await api_mod.send_admin_broadcast(
            api_mod.BroadcastRequest(message="Batch BS token check"),
            _user(ADMIN_ID),
            db_session,
        )
    assert resp["sent_count"] == 1
    campaigns = (await db_session.execute(select(BroadcastCampaign))).scalars().all()
    assert len(campaigns) == 1
    assert len(campaigns[0].confirmation_token) <= 32


# ── Token ──────────────────────────────────────────────────────────


def test_bs_cache_token_is_r90():
    assert INDEX_HTML.count("20261116_r157") == 4
    assert "20261104_r89" not in INDEX_HTML
