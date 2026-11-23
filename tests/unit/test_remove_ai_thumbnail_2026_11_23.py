"""Part E — Remove AI Thumbnail feature end-to-end (2026-11-23) unit test suite."""
from __future__ import annotations

import json
from pathlib import Path

from app.config import settings
from app.models import schema as schema_mod
from app.services import ai_prompt_registry, dynamic_settings
from app.webapp import api as api_mod, rate_limit

ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")
MY_LOCALE = json.loads((ROOT / "locales/my/messages.json").read_text(encoding="utf-8"))
EN_LOCALE = json.loads((ROOT / "locales/en/messages.json").read_text(encoding="utf-8"))


def test_ai_thumbnail_service_endpoint_schema_settings_removed():
    assert not (ROOT / "app/services/recap_thumbnail.py").exists()
    assert not hasattr(schema_mod, "RecapThumbnailPoolUsage")
    assert not hasattr(api_mod, "create_recap_ai_thumbnail")
    assert not hasattr(api_mod, "_send_recap_thumbnail_telegram")
    assert not hasattr(api_mod, "_safe_get_monthly_thumbnail_usage")

    routes = {getattr(r, "path", "") for r in api_mod.api_router.routes}
    assert "/api/recap/{job_id}/thumbnail" not in routes
    assert "/recap/{job_id}/thumbnail" not in routes
    assert "/api/recap/{job_id}/thumbnail" not in rate_limit.RATE_LIMITS

    for key in (
        "recap_thumbnail_monthly_cap_usd",
        "recap_thumbnail_model",
        "recap_thumbnail_prompt_template",
    ):
        assert key not in dynamic_settings.SETTING_TYPES
        assert not hasattr(settings, key)

    assert "recap_thumbnail_brain" not in ai_prompt_registry.PROMPT_REGISTRY_CATALOG


def test_ai_thumbnail_ui_i18n_and_locales_removed():
    for token in (
        "recap-ai-thumbnail-box",
        "btn-recap-ai-thumbnail",
        "recap-thumb-headline-input",
        "adm-ai-thumbnail-card",
        "btn-save-ai-thumbnail-settings",
    ):
        assert token not in INDEX_HTML, f"Found {token} in index.html"

    for token in (
        "generateRecapAiThumbnail",
        "syncRecapAiThumbnailUI",
        "saveAiThumbnailSettings",
        "recap-hist-thumb-box",
        "canUseAiThumbnail",
        "aiThumbnailCapReached",
    ):
        assert token not in APP_JS, f"Found {token} in app.js"

    for token in (
        "btn_recap_ai_thumbnail",
        "btn_recap_dl_thumbnail",
        "hist_dl_thumbnail",
        "recap_thumb_headline_lbl",
        "recap_thumb_free_perk_note",
        "adm_sec_ai_thumbnail",
        "adm_thumb_usage_title",
    ):
        assert token not in I18N_JS, f"Found {token} in i18n.js"

    assert ".recap-ai-thumb-panel" not in STYLE_CSS
    assert ".recap-hist-thumb-box" not in STYLE_CSS

    for key in (
        "recap_thumb_cap_reached",
        "recap_thumb_vip_only",
        "recap_thumb_failed",
        "recap_thumb_telegram_caption",
    ):
        assert key not in MY_LOCALE
        assert key not in EN_LOCALE


def test_shared_recap_deliverables_and_preview_frames_preserved():
    # Step 4 / Result Box deliverables MUST stay intact
    for keep_id in (
        'id="recap-step4-thumb"',
        'id="btn-recap-dl-video"',
        'id="btn-recap-dl-audio"',
        'id="btn-recap-dl-srt"',
        'id="btn-recap-dl-script"',
        'id="recap-expiry-note"',
    ):
        assert keep_id in INDEX_HTML, f"Missing preserved element {keep_id}"

    # Shared preview frame endpoints and Job.final_script MUST stay intact
    routes = {getattr(r, "path", "") for r in api_mod.api_router.routes}
    assert "/recap/frame" in routes or "/api/recap/frame" in routes
    assert "/recap/preview-frame" in routes or "/api/recap/preview-frame" in routes
    assert hasattr(schema_mod.Job, "final_script")
