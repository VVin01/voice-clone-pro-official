"""Mini App Gemini TTS voice picker — all 10 voices + preview samples.

Pins the contract of feat/gemini-voice-preview (2026-11-01):
1. #recap-gemini-voice-select offers EVERY voice the engine supports
   (gemini_tts_engine.VOICES), values unchanged (API contract), with
   the owner's premium Burmese display labels (star names no longer
   shown); Auto stays first, males grouped before females.
2. Every selectable voice has its owner-approved preview MP3 shipped
   in the statics at audio/gemini/preview_<voice>.mp3.
3. The preview button + app.js playback wiring exist and mirror the
   persona preview controller (shared stop function / player state,
   Auto resolving exactly like the API: female persona -> Kore,
   otherwise Charon).
"""
from __future__ import annotations

import re
from pathlib import Path

from app.services.recap_engine.gemini_tts_engine import GEMINI_VOICE_GENDER, GEMINI_VOICE_NAMES

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = REPO_ROOT / "app" / "webapp" / "static"

# Owner-approved display labels (2026-11-01). Values stay the engine /
# API names; only this visible text is premium-named, so a rename is a
# one-line edit here and in index.html.
DISPLAY_LABELS = {
    "Charon": "ကိုသီဟ",
    "Algenib": "ကိုအာကာ",
    "Orus": "ကိုမင်းခန့်",
    "Iapetus": "ကိုလင်းခန့်",
    "Puck": "ကိုသော်တာ",
    "Kore": "မစန္ဒာ",
    "Leda": "မနှင်းဆီ",
    "Sulafat": "မမေသူ",
    "Gacrux": "မသီရိ",
    "Achernar": "မသွန်းရည်",
}

GENDER_TAGS = {"male": "ကျား", "female": "မ"}


def _gemini_select_options() -> list[tuple[str, str]]:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    m = re.search(r'<select id="recap-gemini-voice-select"[^>]*>(.*?)</select>', html, re.S)
    assert m, "recap-gemini-voice-select not found in index.html"
    return [(value, label.strip()) for value, label in
            re.findall(r'<option value="([^"]*)">([^<]*)</option>', m.group(1))]


# ── 1. Dropdown completeness + labels ────────────────────────────────


def test_gemini_select_offers_all_engine_voices():
    options = _gemini_select_options()
    assert options[0][0] == "", "Auto must stay the first option"
    values = [v for v, _ in options if v]
    assert len(values) == 10
    assert set(values) == set(GEMINI_VOICE_NAMES)


def test_gemini_select_groups_males_before_females():
    values = [v for v, _ in _gemini_select_options() if v]
    genders = [GEMINI_VOICE_GENDER[v] for v in values]
    assert genders == ["male"] * 5 + ["female"] * 5


def test_gemini_select_labels_are_owner_burmese_names():
    for value, label in _gemini_select_options():
        if not value:
            assert label == "Auto (ကျား/မ အလိုက်)"
            continue
        expected = f"{DISPLAY_LABELS[value]} ({GENDER_TAGS[GEMINI_VOICE_GENDER[value]]})"
        assert label == expected, (value, label)
        # The English star name must not leak into the visible label.
        assert value not in label


# ── 2. Preview assets ────────────────────────────────────────────────


def test_every_gemini_voice_has_preview_file():
    for name in GEMINI_VOICE_NAMES:
        f = STATIC_DIR / "audio" / "gemini" / f"preview_{name.lower()}.mp3"
        assert f.is_file(), f"missing preview for {name}: {f}"
        data = f.read_bytes()
        assert len(data) > 50_000, f"preview for {name} suspiciously small: {len(data)}"
        # MP3 framing: ID3 tag or an MPEG audio sync word.
        assert data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"), name


def test_gemini_preview_dir_has_exactly_the_ten_samples():
    files = sorted(p.name for p in (STATIC_DIR / "audio" / "gemini").glob("*.mp3"))
    assert files == sorted(f"preview_{n.lower()}.mp3" for n in GEMINI_VOICE_NAMES)


# ── 3. Preview button + playback wiring ──────────────────────────────


def test_gemini_preview_button_markup():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="btn-preview-gemini-voice"' in html
    assert 'onclick="previewSelectedGeminiVoice()"' in html
    # Same button anatomy as the persona recap preview button, so the
    # shared controller's icon/label state resets apply unchanged.
    assert 'class="preview-recap-voice-btn" id="btn-preview-gemini-voice"' in html


def test_gemini_preview_playback_wiring():
    js = (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
    assert "function previewSelectedGeminiVoice()" in js
    assert "async function playGeminiVoicePreview(" in js
    assert "function resolveGeminiPreviewVoice()" in js
    # Static sample path template, one file per voice.
    assert '"/static/audio/gemini"' in js
    assert "preview_${voiceName.toLowerCase()}.mp3" in js
    # Mirrors the persona preview controller: shared stop + fetch→blob.
    assert "stopCurrentVoicePreview" in js
    assert "fetchAuthImageUrl" in js


def test_gemini_preview_auto_resolution_mirrors_api():
    js = (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
    m = re.search(r"function resolveGeminiPreviewVoice\(\) \{(.*?)\n\}", js, re.S)
    assert m, "resolveGeminiPreviewVoice not found"
    body = m.group(1)
    assert 'isFemale ? "Kore" : "Charon"' in body
    # Same female-persona fallback slug list as app/webapp/api.py.
    for slug in ("mahnin", "nilar", "mamay", "manadi", "mayoon", "machaw", "mathiri"):
        assert f'"{slug}"' in body


def test_gemini_select_change_stops_playing_preview():
    js = (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
    m = re.search(r'geminiVoiceSel\.addEventListener\("change", \(\) => \{(.*?)\}\);', js, re.S)
    assert m, "gemini voice change handler not found"
    assert 'currentPreviewButton.id === "btn-preview-gemini-voice"' in m.group(1)
    assert "stopCurrentVoicePreview()" in m.group(1)
