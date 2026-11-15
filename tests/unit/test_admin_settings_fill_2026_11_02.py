"""Mini App Admin settings fill (2026-11-02).

Several runtime settings the bot's chat admin menu can edit were not
editable in the Mini App at all: they were missing from
AdminSettingsPayload AND from the GET /admin/settings response, so a
POST silently dropped them (exclude_unset + pydantic ignoring the
unknown key) — the owner hit exactly this with
character_limit_purchased: POST {"character_limit_purchased": 5000}
returned success with "updated": {} and the limit stayed 2000.

This pins the fill end to end for the nine gap keys:

- registry + config defaults for every key (no registry changes);
- the payload model accepts every key (regression pin for the
  purchased-limit silent drop: the key must appear in "updated");
- audited round-trips through POST /admin/settings (BotSetting row +
  in-memory apply + AuditLog + GET read-back) for every key;
- purchased-limit validation bounds mirroring the free limit;
- the Admin → Settings UI: controls, load/save wiring, my+en i18n;
- the statics token bump (?v=20261104_r61).

gemini_fallback_2 / gemini_fallback_3 were audited too: they are
already on the payload and in the GET response, so they are
deliberately NOT part of this fill.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.config.settings import Settings
from app.models.schema import AuditLog, BotSetting, User
from app.services.dynamic_settings import SETTING_TYPES
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
API_PY = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")

ADMIN_ID = 999002

# key -> (registry type, declared config default)
INT_KEYS = {
    "character_limit_purchased": 5000,
    "free_monthly_allowance_seconds": 180,
    "free_daily_job_limit": 0,  # Batch BZ: retired default (mechanism stays, 0 = off)
    "recap_free_max_duration_seconds": 120,
    "recap_max_duration_seconds": 600,
}
STR_KEYS = {
    "required_channel_id": "",
    "required_group_id": "",
    "channel_invite_link": "https://t.me/voiceclonepro",
    "group_invite_link": "https://t.me/voiceclonepro_chat",
}
ALL_KEYS = {**INT_KEYS, **STR_KEYS}

FIELD_IDS = {
    "character_limit_purchased": "adm-set-char-limit-purchased",
    "free_monthly_allowance_seconds": "adm-set-free-allowance",
    "free_daily_job_limit": "adm-set-daily-job-limit",
    "recap_free_max_duration_seconds": "adm-set-recap-free-max",
    "recap_max_duration_seconds": "adm-set-recap-max",
    "required_channel_id": "adm-set-required-channel-id",
    "required_group_id": "adm-set-required-group-id",
    "channel_invite_link": "adm-set-channel-invite-link",
    "group_invite_link": "adm-set-group-invite-link",
}

I18N_KEYS = [
    "settings_char_limit_purchased",
    "settings_char_limit_purchased_hint",
    "settings_free_allowance",
    "settings_free_allowance_hint",
    "settings_daily_job_limit",
    "settings_daily_job_limit_hint",
    "settings_recap_free_max",
    "settings_recap_free_max_hint",
    "settings_recap_max",
    "settings_recap_max_hint",
    "settings_required_channel_id",
    "settings_required_group_id",
    "settings_channel_invite_link",
    "settings_group_invite_link",
    "settings_gates_hint",
]


def _user(uid: int) -> User:
    return User(id=uid, first_name="Test", username=None,
                account_category="FREE", language_code="my")


def _snapshot() -> dict:
    return {key: getattr(settings, key) for key in ALL_KEYS}


def _restore(snap: dict) -> None:
    for key, val in snap.items():
        setattr(settings, key, val)


# ── Registry + config defaults (registry itself untouched) ─────────

def test_gap_keys_registered_with_types():
    for key in INT_KEYS:
        assert SETTING_TYPES[key] is int, key
    for key in STR_KEYS:
        assert SETTING_TYPES[key] is str, key


def test_gap_key_config_defaults():
    for key, default in ALL_KEYS.items():
        assert Settings.model_fields[key].default == default, key


# ── Payload model accepts every gap key ────────────────────────────

def test_payload_model_accepts_gap_keys():
    sample = {
        "character_limit_purchased": 5000,
        "free_monthly_allowance_seconds": 300,
        "free_daily_job_limit": 10,
        "recap_free_max_duration_seconds": 300,
        "recap_max_duration_seconds": 900,
        "required_channel_id": "-1004459167607",
        "required_group_id": "-1003795677289",
        "channel_invite_link": "https://t.me/VoiceClonePro",
        "group_invite_link": "https://t.me/+2udU2fb1zNiZmI1",
    }
    payload = api_mod.AdminSettingsPayload(**sample)
    dumped = payload.model_dump(exclude_unset=True)
    for key, value in sample.items():
        assert dumped[key] == value, key


# ── GET returns every gap key with its live value ──────────────────

@pytest.mark.asyncio
async def test_get_admin_settings_returns_gap_keys(db_session):
    admin = _user(ADMIN_ID)
    out = await api_mod.get_admin_settings(admin, db_session)
    for key in ALL_KEYS:
        assert key in out, key
        assert out[key] == getattr(settings, key), key
    # int keys come back as ints, not strings
    for key in INT_KEYS:
        assert isinstance(out[key], int), key


# ── REGRESSION PIN: the purchased-limit silent drop ────────────────

@pytest.mark.asyncio
async def test_purchased_limit_post_regression(db_session):
    """POST {"character_limit_purchased": 5000} must actually apply.

    Before the fill, the key was not on AdminSettingsPayload, so the
    POST returned {"success": true, "updated": {}} and the live limit
    stayed at its old value with no error anywhere.
    """
    admin = _user(ADMIN_ID)
    db_session.add(admin)
    await db_session.commit()
    snap = _snapshot()
    try:
        payload = api_mod.AdminSettingsPayload(character_limit_purchased=5000)
        res = await api_mod.update_admin_settings(payload, admin, db_session)
        assert res["success"] is True
        # the key is confirmed in the response — NOT silently dropped
        assert res["updated"].get("character_limit_purchased") == 5000

        # persisted through the audited store
        row = (await db_session.execute(
            select(BotSetting).where(
                BotSetting.key == "character_limit_purchased"))).scalar_one()
        assert row.value == "5000"
        assert settings.character_limit_purchased == 5000
        log = (await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "settings.update",
                AuditLog.target_id == "character_limit_purchased",
            ))).scalar_one()
        assert log.admin_id == ADMIN_ID

        out = await api_mod.get_admin_settings(admin, db_session)
        assert out["character_limit_purchased"] == 5000
    finally:
        _restore(snap)


# ── Audited round-trips for every gap key ──────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("key,value", [
    ("character_limit_purchased", 3000),
    ("free_monthly_allowance_seconds", 300),
    ("free_daily_job_limit", 10),
    ("recap_free_max_duration_seconds", 300),
    ("recap_max_duration_seconds", 900),
])
async def test_int_key_round_trip(db_session, key, value):
    admin = _user(ADMIN_ID)
    db_session.add(admin)
    await db_session.commit()
    snap = _snapshot()
    try:
        payload = api_mod.AdminSettingsPayload(**{key: value})
        res = await api_mod.update_admin_settings(payload, admin, db_session)
        assert res["updated"][key] == value

        row = (await db_session.execute(
            select(BotSetting).where(BotSetting.key == key))).scalar_one()
        assert row.value == str(value)
        assert getattr(settings, key) == value

        out = await api_mod.get_admin_settings(admin, db_session)
        assert out[key] == value
    finally:
        _restore(snap)


@pytest.mark.asyncio
@pytest.mark.parametrize("key,value", [
    ("required_channel_id", "-1004459167607"),
    ("required_group_id", "-1003795677289"),
    ("channel_invite_link", "https://t.me/VoiceClonePro"),
    ("group_invite_link", "https://t.me/+2udU2fb1zNiZmI1"),
])
async def test_str_key_round_trip(db_session, key, value):
    admin = _user(ADMIN_ID)
    db_session.add(admin)
    await db_session.commit()
    snap = _snapshot()
    try:
        payload = api_mod.AdminSettingsPayload(**{key: value})
        res = await api_mod.update_admin_settings(payload, admin, db_session)
        assert res["updated"][key] == value

        row = (await db_session.execute(
            select(BotSetting).where(BotSetting.key == key))).scalar_one()
        assert row.value == value
        assert getattr(settings, key) == value

        out = await api_mod.get_admin_settings(admin, db_session)
        assert out[key] == value
    finally:
        _restore(snap)


# ── Purchased-limit validation mirrors the free limit ──────────────

def test_purchased_limit_validation_bounds():
    out = api_mod._validate_admin_settings_updates(
        {"character_limit_purchased": 5000})
    assert out["character_limit_purchased"] == 5000
    for bad in (0, 50, 99, 10001, "abc"):
        with pytest.raises(HTTPException):
            api_mod._validate_admin_settings_updates(
                {"character_limit_purchased": bad})


# ── Admin → Settings UI: controls, wiring, i18n ────────────────────

def test_admin_settings_ui_controls_present():
    for key, el_id in FIELD_IDS.items():
        assert f'id="{el_id}"' in INDEX_HTML, el_id
    # numeric gap fields are number inputs like the free-limit editor
    for el_id in ("adm-set-char-limit-purchased", "adm-set-free-allowance",
                  "adm-set-daily-job-limit", "adm-set-recap-free-max",
                  "adm-set-recap-max"):
        assert f'type="number" id="{el_id}"' in INDEX_HTML, el_id
    # every new label/hint is i18n-bound in the markup
    for key in I18N_KEYS:
        assert f'data-i18n="{key}"' in INDEX_HTML, key
    # the gate fields carry the caution hint next to them
    assert 'data-i18n="settings_gates_hint"' in INDEX_HTML


def test_admin_settings_js_wiring():
    load_body = APP_JS.split("async function loadAdminSettings")[1].split(
        "async function saveGeminiModelSettings")[0]
    save_body = APP_JS.split("async function saveAdminSettings")[1].split(
        "\n}")[0]
    for key, el_id in FIELD_IDS.items():
        assert f'"{el_id}"' in APP_JS, el_id
        assert f"s.{key}" in load_body, key
    for fragment in (
        "character_limit_purchased: charLimitPurchased",
        "free_monthly_allowance_seconds: freeAllowance",
        "free_daily_job_limit: dailyJobLimit",
        "recap_free_max_duration_seconds: recapFreeMax",
        "recap_max_duration_seconds: recapMax",
        "required_channel_id: reqChannelId",
        "required_group_id: reqGroupId",
        "channel_invite_link: channelInvite",
        "group_invite_link: groupInvite",
    ):
        assert fragment in save_body, fragment


def test_i18n_keys_present_both_languages():
    for key in I18N_KEYS:
        # once in the my block, once in the en block
        assert I18N_JS.count(f"{key}:") >= 2, key


def test_api_source_carries_gap_keys():
    assert "character_limit_purchased: Optional[int] = None" in API_PY
    assert ('"character_limit_purchased": int(getattr(settings, '
            '"character_limit_purchased", 5000) or 5000)') in API_PY
    # the already-present gemini fallbacks were not disturbed
    assert "gemini_fallback_2: Optional[str] = None" in API_PY
    assert "gemini_fallback_3: Optional[str] = None" in API_PY


def test_statics_token_bumped():
    assert INDEX_HTML.count("?v=20261115_r152") == 4
    assert "20261102_ga1" not in INDEX_HTML
