"""Phase 7 verification tests — Admin Console, TTS Model Chain & Settings Gap Fill.

Covers:
(a) Gemini TTS Model Chain:
    - 4 dynamic settings (`gemini_tts_primary_model`, `gemini_tts_fallback_1`,
      `gemini_tts_lite_model`, `gemini_tts_fallback_2`) registered in Settings
      & SETTING_TYPES with exact day-1 defaults reproducing `TTS_MODELS`.
    - `_effective_tts_models()` and `_rotated_tts_models()` honour runtime
      overrides while preserving `_TTS_MODEL_CURSOR` round-robin rotation.
    - Admin GET/POST `/api/admin/settings` round-trip, validation, and UI
      dropdowns + custom entry (`adm-tts-*` + `saveGeminiTtsModelSettings`).
(b) All 6 gap dynamic settings exposed end-to-end:
    - `voice_daily_limit_minutes_free`, `voice_daily_limit_minutes_paid`,
      `tts_daily_limit_minutes_free`, `tts_daily_limit_minutes_paid`,
      `tutorial_video_enabled`, and `voxcpm_timeout` (added to backend
      AdminSettingsPayload, GET, validation 10..600, plus Admin UI/JS).
(c) Admin Console completeness & design pass:
    - `require_admin` enforced on `/api/admin/*` endpoints.
    - User detail completeness (`jobs_active`, `is_test_account`, etc.).
    - Phase 7 CSS rules (`@media (max-width: 420px)` + `html.lite`), `my`/`en`
      100% i18n parity, and static cache token intact.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config.settings import Settings, settings
from app.models.enums import AccountCategory
from app.models.schema import AuditLog, BotSetting, User
from app.services.dynamic_settings import SETTING_TYPES
from app.services.recap_engine import gemini_tts_engine as gte
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "webapp" / "static"
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (STATIC / "css" / "style.css").read_text(encoding="utf-8")
APP_JS = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")

ADMIN_ID = settings.admin_ids_list[0] if hasattr(settings, "admin_ids_list") and settings.admin_ids_list else (next(iter(settings.admin_ids)) if settings.admin_ids else 999001)


def _extract_i18n_block(locale: str) -> dict[str, str]:
    script = (
        I18N_JS.split("let currentLang =", 1)[0]
        + f"\nconsole.log(JSON.stringify(translations[{json.dumps(locale)}]));\n"
    )
    res = subprocess.check_output(["node", "-"], input=script, text=True, encoding="utf-8")
    return json.loads(res)


def _user(uid: int) -> User:
    return User(
        id=uid,
        first_name="AdminTest",
        username=f"u{uid}",
        account_category=AccountCategory.PURCHASED.value,
        language_code="my",
    )


# ── (a) TTS Model Chain Dynamic Settings ──────────────────────────────


def test_tts_chain_seeded_defaults_match_production_tuple_verbatim():
    expected = (
        "gemini-3.8-flash-tts",
        "gemini-3.1-flash-tts-preview",
        "gemini-3.8-flash-lite-tts",
        "gemini-2.5-flash-preview-tts",
    )
    assert Settings.model_fields["gemini_tts_primary_model"].default == expected[0]
    assert Settings.model_fields["gemini_tts_fallback_1"].default == expected[1]
    assert Settings.model_fields["gemini_tts_lite_model"].default == expected[2]
    assert Settings.model_fields["gemini_tts_fallback_2"].default == expected[3]
    for key in (
        "gemini_tts_primary_model",
        "gemini_tts_fallback_1",
        "gemini_tts_lite_model",
        "gemini_tts_fallback_2",
    ):
        assert SETTING_TYPES[key] is str
    assert gte.TTS_MODELS == expected
    assert gte._effective_tts_models() == expected


def test_tts_chain_runtime_override_and_rotation(monkeypatch):
    gte._TTS_MODEL_CURSOR = 0
    monkeypatch.setattr(settings, "gemini_tts_primary_model", "gemini-custom-tts-1")
    monkeypatch.setattr(settings, "gemini_tts_fallback_1", "gemini-custom-tts-2")
    monkeypatch.setattr(settings, "gemini_tts_lite_model", "")
    monkeypatch.setattr(settings, "gemini_tts_fallback_2", "gemini-custom-tts-3")

    eff = gte._effective_tts_models()
    assert eff == ("gemini-custom-tts-1", "gemini-custom-tts-2", "gemini-custom-tts-3")

    rot1 = gte._rotated_tts_models()
    rot2 = gte._rotated_tts_models()
    rot3 = gte._rotated_tts_models()
    rot4 = gte._rotated_tts_models()
    assert rot1 == ("gemini-custom-tts-1", "gemini-custom-tts-2", "gemini-custom-tts-3")
    assert rot2 == ("gemini-custom-tts-2", "gemini-custom-tts-3", "gemini-custom-tts-1")
    assert rot3 == ("gemini-custom-tts-3", "gemini-custom-tts-1", "gemini-custom-tts-2")
    assert rot4 == rot1
    gte._TTS_MODEL_CURSOR = 0


@pytest.mark.asyncio
async def test_admin_settings_tts_chain_and_voxcpm_timeout_round_trip(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    admin = _user(ADMIN_ID)
    db_session.add(admin)
    await db_session.commit()

    snap = {
        k: getattr(settings, k)
        for k in (
            "gemini_tts_primary_model",
            "gemini_tts_fallback_1",
            "gemini_tts_lite_model",
            "gemini_tts_fallback_2",
            "voxcpm_timeout",
        )
    }
    try:
        before = await api_mod.get_admin_settings(admin, db_session)
        assert (
            before["gemini_tts_primary_model"],
            before["gemini_tts_fallback_1"],
            before["gemini_tts_lite_model"],
            before["gemini_tts_fallback_2"],
        ) == gte.TTS_MODELS
        assert before["voxcpm_timeout"] == 60

        payload = api_mod.AdminSettingsPayload(
            gemini_tts_primary_model="gemini-3.9-flash-tts",
            gemini_tts_fallback_1="gemini-3.8-flash-tts",
            gemini_tts_lite_model="gemini-3.8-flash-lite-tts",
            gemini_tts_fallback_2="gemini-2.5-flash-preview-tts",
            voxcpm_timeout=95,
        )
        res = await api_mod.update_admin_settings(payload, admin, db_session)
        assert res["success"] is True
        assert res["updated"]["gemini_tts_primary_model"] == "gemini-3.9-flash-tts"
        assert res["updated"]["voxcpm_timeout"] == 95
        assert gte._effective_tts_models()[0] == "gemini-3.9-flash-tts"

        row = (await db_session.execute(
            select(BotSetting).where(BotSetting.key == "voxcpm_timeout")
        )).scalar_one()
        assert row.value == "95"

        audit = (await db_session.execute(
            select(AuditLog).where(AuditLog.target_id == "gemini_tts_primary_model")
        )).scalars().first()
        assert audit is not None
    finally:
        for k, v in snap.items():
            setattr(settings, k, v)


def test_tts_chain_and_voxcpm_timeout_validation_bounds():
    with pytest.raises(HTTPException) as e_empty:
        api_mod._validate_admin_settings_updates({"gemini_tts_primary_model": "   "})
    assert e_empty.value.status_code == 400

    with pytest.raises(HTTPException) as e_long:
        api_mod._validate_admin_settings_updates({"gemini_tts_fallback_1": "x" * 201})
    assert e_long.value.status_code == 400

    for bad_timeout in (0, 9, 601, "invalid"):
        with pytest.raises(HTTPException) as e_to:
            api_mod._validate_admin_settings_updates({"voxcpm_timeout": bad_timeout})
        assert e_to.value.status_code == 400

    valid = api_mod._validate_admin_settings_updates(
        {"voxcpm_timeout": "120", "gemini_tts_lite_model": "  custom-tts  "}
    )
    assert valid["voxcpm_timeout"] == 120
    assert valid["gemini_tts_lite_model"] == "custom-tts"


# ── (b) Six Missing Dynamic Settings UI/JS Wiring + TTS Chain UI ──────


def test_six_gap_settings_and_tts_chain_controls_in_html_and_js():
    admin_html = INDEX_HTML.split('<section id="tab-admin" class="tab-pane hidden">', 1)[1].split("</section>", 1)[0]

    for el_id in (
        "adm-set-voice-daily-free",
        "adm-set-voice-daily-paid",
        "adm-set-tts-daily-free",
        "adm-set-tts-daily-paid",
        "adm-set-voxcpm-timeout",
        "adm-set-tutorial-video-enabled",
        "adm-tts-chain-card",
        "adm-tts-primary",
        "adm-tts-primary-custom",
        "adm-tts-fb1",
        "adm-tts-fb1-custom",
        "adm-tts-lite",
        "adm-tts-lite-custom",
        "adm-tts-fb2",
        "adm-tts-fb2-custom",
        "btn-save-gemini-tts-chain",
    ):
        assert f'id="{el_id}"' in admin_html, el_id

    load_body = APP_JS.split("async function loadAdminSettings", 1)[1].split(
        "async function saveGeminiModelSettings", 1
    )[0]
    save_body = APP_JS.split("async function saveAdminSettings", 1)[1].split("\n}\n", 1)[0]
    tts_save_body = APP_JS.split("async function saveGeminiTtsModelSettings", 1)[1].split("\n}\n", 1)[0]

    for key in (
        "voice_daily_limit_minutes_free",
        "voice_daily_limit_minutes_paid",
        "tts_daily_limit_minutes_free",
        "tts_daily_limit_minutes_paid",
        "voxcpm_timeout",
        "tutorial_video_enabled",
        "gemini_tts_primary_model",
        "gemini_tts_fallback_1",
        "gemini_tts_lite_model",
        "gemini_tts_fallback_2",
    ):
        assert f"s.{key}" in load_body, key

    for key in (
        "voice_daily_limit_minutes_free:",
        "voice_daily_limit_minutes_paid:",
        "tts_daily_limit_minutes_free:",
        "tts_daily_limit_minutes_paid:",
        "voxcpm_timeout:",
        "tutorial_video_enabled:",
    ):
        assert key in save_body, key

    for key in (
        "gemini_tts_primary_model:",
        "gemini_tts_fallback_1:",
        "gemini_tts_lite_model:",
        "gemini_tts_fallback_2:",
    ):
        assert key in tts_save_body, key

    # Dirty-guard preservation check (text save boundary untouched)
    assert "adminSettingsDirty.clear()" not in tts_save_body


@pytest.mark.asyncio
async def test_admin_settings_non_admin_rejected_server_side(db_session, monkeypatch):
    import time

    non_admin = _user(888777)
    db_session.add(non_admin)
    await db_session.commit()
    monkeypatch.setattr(settings, "admin_ids_raw", "999001")

    with pytest.raises(HTTPException) as exc_missing:
        api_mod.require_admin(x_telegram_init_data=None, user=non_admin)
    assert exc_missing.value.status_code == 403

    monkeypatch.setattr(api_mod, "parse_and_verify_init_data", lambda _raw: {"id": non_admin.id})
    monkeypatch.setattr(api_mod, "init_data_auth_date", lambda _raw: time.time())
    with pytest.raises(HTTPException) as exc_non_admin:
        api_mod.require_admin(x_telegram_init_data="signed-non-admin", user=non_admin)
    assert exc_non_admin.value.status_code == 403
    assert "Admin authorization required" in exc_non_admin.value.detail


# ── (c) Admin Security, User Completeness, Design Pass & i18n ─────────


def test_all_admin_routes_require_admin_server_side():
    missing = []
    for route in api_mod.api_router.routes:
        if "/admin" in route.path:
            deps = [
                getattr(d.call, "__name__", str(d.call))
                for d in route.dependant.dependencies
            ]
            if "require_admin" not in deps:
                missing.append(route.path)
    assert missing == [], f"Admin routes missing require_admin: {missing}"


def test_admin_design_pass_i18n_parity_and_user_detail_completeness():
    # User detail displays active job & test account badges when present
    assert "u.is_test_account" in APP_JS
    assert "admin_badge_test_acct" in APP_JS
    assert "u.jobs_active" in APP_JS
    assert "admin_badge_active_jobs" in APP_JS

    # Phase 7 CSS design pass & lite mode rules present
    p7_css = STYLE_CSS.split("PHASE 7: ADMIN CONSOLE DESIGN PASS", 1)[1]
    assert "#tab-admin .vcp-card" in p7_css
    assert "@media (max-width: 420px)" in p7_css
    assert "html.lite #tab-admin .adm-nav" in p7_css

    # 100% my + en i18n parity & Phase 7 keys present
    my = _extract_i18n_block("my")
    en = _extract_i18n_block("en")
    assert set(my.keys()) == set(en.keys())
    for key in (
        "settings_voice_daily_free",
        "settings_voice_daily_free_hint",
        "settings_voice_daily_paid",
        "settings_voice_daily_paid_hint",
        "settings_tts_daily_free",
        "settings_tts_daily_free_hint",
        "settings_tts_daily_paid",
        "settings_tts_daily_paid_hint",
        "settings_voxcpm_timeout",
        "settings_voxcpm_timeout_hint",
        "settings_tutorial_video_enabled",
        "settings_tutorial_video_enabled_hint",
        "adm_gemini_tts_title",
        "adm_gemini_tts_note",
        "adm_gemini_tts_primary",
        "adm_gemini_tts_fb1",
        "adm_gemini_tts_lite",
        "adm_gemini_tts_fb2",
        "adm_gemini_tts_save",
        "admin_tts_chain_saved",
        "admin_tts_chain_save_err",
        "admin_badge_test_acct",
        "admin_badge_active_jobs",
    ):
        assert key in my and my[key], f"missing my.{key}"
        assert key in en and en[key], f"missing en.{key}"

    assert INDEX_HTML.count("20261114_r149") == 4
