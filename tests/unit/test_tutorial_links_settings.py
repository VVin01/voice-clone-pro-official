"""All tutorial links admin-editable (premium polish follow-up).

Until now only the video/voice/recap tutorial links were settings; the
other seven (+ the index post) were frontend-registry constants. This
pins the extension end to end, mirroring the original three exactly:

- config defaults + dynamic-settings registry entries for the 8 new keys;
- audited round-trip through set_dynamic_setting (BotSetting row +
  in-memory apply + AuditLog), the same store POST /admin/settings uses;
- POST /admin/settings accepts the new keys and GET returns them;
- GET /api/user/me carries the new keys (DB value wins over default);
- frontend resolution is settings-first for a non-original key (plans):
  updateTutorialUI ingests tutorial_plans_link and openTutorialVideo
  resolves it through resolveTutorialLink, with the Admin → Settings
  inputs wired into the existing load/save flow.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
from app.config.settings import Settings
from app.models.schema import AuditLog, BotSetting, User
from app.services.dynamic_settings import (
    SETTING_TYPES,
    get_dynamic_setting,
    set_dynamic_setting,
)
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")

ADMIN_ID = 999001

NEW_KEYS = {
    "tutorial_index_link": "https://t.me/VoiceClonePro/38",
    "tutorial_plans_link": "https://t.me/VoiceClonePro/31",
    "tutorial_customvoice_link": "https://t.me/VoiceClonePro/32",
    "tutorial_downloader_link": "https://t.me/VoiceClonePro/33",
    "tutorial_scriptedit_link": "https://t.me/VoiceClonePro/34",
    "tutorial_recapadvanced_link": "https://t.me/VoiceClonePro/35",
    "tutorial_refer_link": "https://t.me/VoiceClonePro/36",
    "tutorial_support_link": "https://t.me/VoiceClonePro/37",
}

FIELD_IDS = {
    "tutorial_index_link": "adm-tut-index-link",
    "tutorial_plans_link": "adm-tut-plans-link",
    "tutorial_customvoice_link": "adm-tut-customvoice-link",
    "tutorial_downloader_link": "adm-tut-downloader-link",
    "tutorial_scriptedit_link": "adm-tut-scriptedit-link",
    "tutorial_recapadvanced_link": "adm-tut-recapadvanced-link",
    "tutorial_refer_link": "adm-tut-refer-link",
    "tutorial_support_link": "adm-tut-support-link",
}


def _user(uid: int) -> User:
    return User(id=uid, first_name="Test", username=None,
                account_category="FREE", language_code="my")


# ── Defaults + registry ──────────────────────────────────────────────

def test_new_tutorial_link_defaults_match_registry():
    fresh = Settings()
    for key, url in NEW_KEYS.items():
        assert getattr(fresh, key) == url, key
        # the API's shared key->default map agrees with the config default
        assert api_mod.TUTORIAL_EXTRA_LINKS[key] == url, key
        # dynamic-settings registry knows the key (load path applies it)
        assert SETTING_TYPES[key] is str, key


# ── Round-trip through the audited settings store ────────────────────

@pytest.mark.asyncio
async def test_new_tutorial_link_round_trip(db_session):
    key = "tutorial_plans_link"
    custom = "https://t.me/VoiceClonePro/99"
    original = getattr(settings, key)
    try:
        await set_dynamic_setting(db_session, key, custom, admin_id=ADMIN_ID)
        await db_session.commit()

        # persisted row + read-back through the same helper /api/user/me uses
        row = (await db_session.execute(
            select(BotSetting).where(BotSetting.key == key))).scalar_one()
        assert row.value == custom
        assert await get_dynamic_setting(db_session, key) == custom
        # applied to the in-memory settings object
        assert getattr(settings, key) == custom
        # audited like every other settings change
        log = (await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "settings.update",
                AuditLog.target_id == key,
            ))).scalar_one()
        assert log.admin_id == ADMIN_ID
    finally:
        setattr(settings, key, original)


# ── Admin settings endpoint accepts + returns the new keys ─────────

@pytest.mark.asyncio
async def test_admin_settings_post_and_get_include_new_keys(db_session):
    admin = _user(ADMIN_ID)
    db_session.add(admin)
    await db_session.commit()
    custom = "https://t.me/VoiceClonePro/98"
    original = getattr(settings, "tutorial_downloader_link")
    try:
        payload = api_mod.AdminSettingsPayload(tutorial_downloader_link=custom)
        res = await api_mod.update_admin_settings(payload, admin, db_session)
        assert res["success"] is True
        assert res["updated"]["tutorial_downloader_link"] == custom

        out = await api_mod.get_admin_settings(admin, db_session)
        assert out["tutorial_downloader_link"] == custom
        # untouched new keys still report their registry defaults
        for key, url in NEW_KEYS.items():
            if key != "tutorial_downloader_link":
                assert out[key] == url, key
    finally:
        setattr(settings, "tutorial_downloader_link", original)


# ── /api/user/me payload carries the new keys ────────────────────────

@pytest.mark.asyncio
async def test_user_me_payload_carries_new_tutorial_links(db_session):
    user = _user(700111)
    db_session.add(user)
    custom = "https://t.me/VoiceClonePro/97"
    # an admin-saved value lives as a BotSetting row; it must win over
    # the default in the payload the app consumes
    db_session.add(BotSetting(key="tutorial_plans_link", value=custom))
    await db_session.commit()

    payload = await api_mod.get_user_profile(user, db_session)
    for key, url in NEW_KEYS.items():
        assert key in payload, key
        assert payload[key] == (custom if key == "tutorial_plans_link" else url), key
    # the original three are untouched by the extension
    assert payload["tutorial_voice_link"] == "https://t.me/VoiceClonePro/29"
    assert payload["tutorial_recap_link"] == "https://t.me/VoiceClonePro/30"


# ── Frontend: settings-first resolution + Admin UI wiring (pins) ─────

def test_frontend_settings_first_for_non_original_key():
    # registry resolution helper exists and openTutorialVideo uses it
    assert "function resolveTutorialLink" in APP_JS
    assert "openTutorialUrl(resolveTutorialLink(type))" in APP_JS
    # the settings key map ties registry keys to their settings keys
    block = APP_JS.split("const TUTORIAL_SETTING_KEYS")[1].split("};")[0]
    for tut_key, setting_key in [
        ("index", "tutorial_index_link"),
        ("plans", "tutorial_plans_link"),
        ("customvoice", "tutorial_customvoice_link"),
        ("downloader", "tutorial_downloader_link"),
        ("scriptedit", "tutorial_scriptedit_link"),
        ("recapadvanced", "tutorial_recapadvanced_link"),
        ("refer", "tutorial_refer_link"),
        ("support", "tutorial_support_link"),
    ]:
        assert f'{tut_key}: "{setting_key}"' in block, tut_key
    # updateTutorialUI ingests the overrides from the /api/user/me payload
    assert "tutorialLinkOverrides[tutKey] = v" in APP_JS
    # an override wins: resolveTutorialLink checks overrides before defaults
    resolve_body = APP_JS.split("function resolveTutorialLink")[1].split("\n}")[0]
    assert resolve_body.index("tutorialLinkOverrides[type]") < resolve_body.index("TUTORIAL_LINKS[type]")


def test_admin_settings_ui_has_all_tutorial_fields():
    for key, el_id in FIELD_IDS.items():
        assert f'id="{el_id}"' in INDEX_HTML, el_id
        # load + save flows both reference the field map entries
        assert f'"{el_id}", "{key}"' in APP_JS, (el_id, key)
    # the original three fields keep their exact ids (existing behaviour)
    for el_id in ("adm-set-tutorial-link", "adm-tut-voice-link", "adm-tut-recap-link",
                  "adm-tut-voice-title", "adm-tut-recap-title",
                  "adm-tut-voice-enabled", "adm-tut-recap-enabled"):
        assert f'id="{el_id}"' in INDEX_HTML, el_id
    # the new fields ride the existing settings save (same button/flow)
    assert "saveAdminSettings()" in INDEX_HTML
    save_body = APP_JS.split("async function saveAdminSettings")[1].split("\n}")[0]
    assert "...tutExtraLinks" in save_body
    assert re.search(r"fetch\(\"/api/admin/settings\"", APP_JS)
