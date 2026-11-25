"""Part G — Admin tab & dynamic_settings reconcile (2026-11-23) unit test suite."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import settings
from app.services import dynamic_settings
from app.services.ledger import get_or_create_user
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
ADMIN_JS = (ROOT / "app/webapp/static/js/admin.js").read_text(encoding="utf-8")


def test_dead_and_abolished_settings_removed_from_registry_and_admin_ui():
    retired_keys = (
        "free_monthly_allowance_seconds",
        "voice_daily_limit_minutes_free",
        "voice_daily_limit_minutes_paid",
        "free_daily_job_limit",
        "tiered_recap_pricing_enabled",
        "tutorial_video_enabled",
        "tutorial_recap_title",
    )
    for k in retired_keys:
        assert k not in dynamic_settings.SETTING_TYPES, f"Retired key {k} still in SETTING_TYPES"
        assert k not in api_mod.AdminSettingsPayload.model_fields, f"Retired key {k} still in AdminSettingsPayload"

    # Retired UI controls removed
    for dead_id in (
        'id="adm-set-voice-daily-free"',
        'id="adm-set-voice-daily-paid"',
        'id="adm-set-daily-job-limit"',
        'id="adm-set-tiered-pricing"',
        'id="adm-set-tutorial-video-enabled"',
        'id="adm-tut-recap-title"',
        'id="adm-set-group-ai"',
        'data-pcat="topup"',
    ):
        assert dead_id not in INDEX_HTML, f"Retired control {dead_id} still in index.html"

    assert '<option value="topup">' not in ADMIN_JS
    assert api_mod._tiered_recap_pricing_enabled() is False


def test_required_live_admin_controls_reachable_and_properly_grouped():
    # Character limit free + purchased MUST both live inside #adm-acc-quotas-gates
    m_quotas = re.search(r'id="adm-acc-quotas-gates"[\s\S]*?</details>', INDEX_HTML)
    assert m_quotas
    quotas_html = m_quotas.group(0)
    for required_id in (
        'id="adm-set-char-limit-free"',
        'id="adm-set-char-limit-purchased"',
        'id="adm-set-free-allowance"',
        'id="adm-set-paid-gemini-taste"',
        'id="adm-set-free-gemini-taste"',
        'id="adm-set-tts-daily-free"',
        'id="adm-set-tts-daily-paid"',
        'id="adm-set-recap-free-max"',
        'id="adm-set-recap-max"',
    ):
        assert required_id in quotas_html, f"Missing {required_id} in #adm-acc-quotas-gates"

    # Misplaced char-limit-free and duplicate group-ai toggle must NOT be in #adm-acc-ai-bots
    m_bots = re.search(r'id="adm-acc-ai-bots"[\s\S]*?</details>', INDEX_HTML)
    assert m_bots
    bots_html = m_bots.group(0)
    assert 'id="adm-set-char-limit-free"' not in bots_html
    assert 'id="adm-set-group-ai"' not in bots_html

    # Single Group AI master toggle + sheet editor present
    assert 'id="adm-group-ai-master-toggle"' in INDEX_HTML
    assert 'id="adm-group-ai-sheet"' in INDEX_HTML
    # Live tutorial_customvoice_link preserved (backed by Home Tutorials Hub)
    assert 'id="adm-tut-customvoice-link"' in INDEX_HTML
    assert "openTutorialVideo('customvoice')" in INDEX_HTML
    # Load-bearing defaults untouched
    assert settings.character_limit_free == 3000
    assert settings.character_limit_purchased == 15000
    assert settings.voice_daily_limit_minutes_free == 0
    assert settings.voice_daily_limit_minutes_paid == 0


@pytest.mark.asyncio
async def test_admin_settings_endpoint_omits_retired_keys_and_user_me_reads_live_recap_max(
    db_session, monkeypatch
):
    admin, _ = await get_or_create_user(db_session, telegram_id=950001)
    monkeypatch.setattr(settings, "admin_ids_raw", str(admin.id))

    resp = await api_mod.get_admin_settings(admin=admin, db=db_session)
    for retired in (
        "free_monthly_allowance_seconds",
        "voice_daily_limit_minutes_free",
        "voice_daily_limit_minutes_paid",
        "free_daily_job_limit",
        "tiered_recap_pricing_enabled",
        "tutorial_video_enabled",
        "tutorial_recap_title",
    ):
        assert retired not in resp

    assert resp["character_limit_free"] == 3000
    assert resp["character_limit_purchased"] == 15000
    assert resp["free_daily_allowance_seconds"] == 180
    assert resp["paid_gemini_taste_minutes"] == 10

    # Verify knowledge-sheet save returns char_limit == 20000 (not stale 8000)
    monkeypatch.setattr(settings, "group_ai_knowledge_sheet", "")
    save_res = await api_mod.update_group_ai_knowledge_sheet(
        api_mod.GroupAiKnowledgeSheetPayload(sheet="Sample ground truth sheet text"),
        admin=admin,
        db=db_session,
    )
    assert save_res["char_limit"] == 20000
