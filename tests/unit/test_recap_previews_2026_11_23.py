"""Part D — Recap Step 3 sample previews, button polish, Edge samples & asset stamp (2026-11-23)."""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.services.ledger import get_or_create_user
from app.services.tts_service import EDGE_TTS_VOICES
from app.webapp import api as api_mod
from app.webapp import server as server_mod

ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")


def test_asset_version_stamp_bumped_and_server_aligned():
    assert "?v=20261118_r164" not in INDEX_HTML
    assert INDEX_HTML.count("?v=20261123_followup1") == 5
    serve_src = inspect.getsource(server_mod.serve_index)
    assert "?v=20261123_followup1" in serve_src
    assert "?v=20261118_r164" not in serve_src


def test_shared_preview_audio_primed_synchronously_and_no_silent_failures():
    assert "VCP_SILENT_WAV_DATA_URI" in APP_JS
    assert "function primeSharedPreviewAudio()" in APP_JS
    assert "function runSharedVoicePreview(" in APP_JS

    # All three preview functions must route through the shared gesture-primed helper
    for fn_name in ("playVoicePreview", "playGeminiVoicePreview", "playEdgeVoicePreview"):
        m = re.search(rf"async function {fn_name}\([\s\S]{{0,900}}\n\}}", APP_JS)
        assert m, f"Missing {fn_name}"
        body = m.group(0)
        assert "runSharedVoicePreview(" in body, f"{fn_name} must use runSharedVoicePreview"
        assert 'showStudioToast(t("preview_load_failed")' in body

    # Inside runSharedVoicePreview, primeSharedPreviewAudio must run BEFORE await fetchAuthImageUrl
    m_shared = re.search(r"async function runSharedVoicePreview\([\s\S]{0,2200}\n\}", APP_JS)
    assert m_shared
    shared_body = m_shared.group(0)
    assert shared_body.index("primeSharedPreviewAudio()") < shared_body.index("await fetchAuthImageUrl(")
    assert "new Audio(" not in shared_body
    # Playback rejection and audio.onerror must both surface preview_load_failed toast
    assert shared_body.count('showStudioToast(t("preview_load_failed")') >= 4


def test_preview_buttons_single_icon_and_unified_step3_full_width():
    # No embedded emojis in preview label strings (icon span owns the single glyph)
    assert 'preview_loading: "နမူနာ ရယူနေသည်…"' in I18N_JS
    assert 'preview_loading: "Loading sample…"' in I18N_JS
    assert 'btn_preview_short: "နားထောင်"' in I18N_JS
    assert 'btn_preview_short: "Preview"' in I18N_JS
    assert 'preview_playing: "ဖွင့်နေသည်…"' in I18N_JS
    assert 'preview_playing: "Playing…"' in I18N_JS

    # All 3 Recap Step 3 preview buttons share .preview-recap-voice-btn + single icon span
    for btn_id in ("btn-preview-gemini-voice", "btn-recap-edge-preview", "btn-recap-persona-preview"):
        m = re.search(rf'<button[^>]*id="{btn_id}"[\s\S]{{0,350}}</button>', INDEX_HTML)
        assert m, f"Missing button {btn_id}"
        snippet = m.group(0)
        assert "preview-recap-voice-btn" in snippet
        assert 'class="preview-icon"' in snippet
        assert 'class="preview-btn-label"' in snippet

    # Old Gemini-only short-pill override must be gone
    assert ".recap-gemini-wrap .preview-recap-voice-btn { align-self: flex-start; }" not in STYLE_CSS
    assert ".recap-persona-wrap .preview-recap-voice-btn" in STYLE_CSS


def test_edge_sample_phonetic_burmese_text_and_all_8_bundled_mp3s():
    expected_text = "မင်္ဂလာပါခင်ဗျာ၊ ဗွိုက်စ် ကလိုန်း ပရို စတူဒီယိုမှ ကြိုဆိုပါတယ်။"
    assert api_mod._EDGE_VOICE_SAMPLE_TEXT == expected_text
    assert "Voice Clone Pro" not in api_mod._EDGE_VOICE_SAMPLE_TEXT

    edge_dir = ROOT / "app/webapp/static/audio/edge"
    for key in EDGE_TTS_VOICES:
        mp3 = edge_dir / f"preview_{key}.mp3"
        assert mp3.is_file(), f"Missing bundled Edge sample {mp3}"
        assert mp3.stat().st_size >= 10_000, f"Bundled Edge sample {mp3} unexpectedly small"


@pytest.mark.asyncio
async def test_edge_sample_endpoint_never_falls_back_to_wrong_persona_voice(
    db_session, tmp_path, monkeypatch
):
    user, _ = await get_or_create_user(db_session, telegram_id=940001)
    # Point root/storage to empty tmp_path and simulate edge_tts failure
    monkeypatch.setattr(api_mod.settings, "root", tmp_path)
    monkeypatch.setattr(api_mod.settings, "storage_root", tmp_path / "storage")

    import builtins
    real_import = builtins.__import__

    def _fail_edge_tts(name, *args, **kwargs):
        if name == "edge_tts":
            raise RuntimeError("simulated offline edge_tts failure")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fail_edge_tts)

    with pytest.raises(HTTPException) as exc_info:
        await api_mod.get_edge_voice_sample_audio("andrew", user=user)

    assert exc_info.value.status_code == 503
