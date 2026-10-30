"""Mini App subtitle font controls + backend integration tests (2026-10-30).

Validates:
1. RecapConfigRequest sub_font validation, defaulting, and aliases
2. Downloader signature and pipeline forwarding for sub_font
3. Mini App HTML control presence and Liquid Glass styling classes
4. Full i18n key parity across Burmese (my) and English (en)
5. app.js state and payload wiring
6. Phone-width audit: no clipped labels or overflow at 360px-480px widths in either language
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
from app.services.downloader import MediaDownloader
from app.services.recap_engine.core import SUBTITLE_FONT_OPTIONS, resolve_subtitle_font
from app.webapp.api import RecapConfigRequest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── 1. Backend Request Validation ──────────────────────────────────────────


def test_recap_config_request_sub_font_default():
    """sub_font defaults to 'default' when omitted."""
    req = RecapConfigRequest(url="https://example.com/video.mp4")
    assert req.sub_font == "default"


def test_recap_config_request_sub_font_choices():
    """All registered fonts in SUBTITLE_FONT_OPTIONS are accepted."""
    for key in SUBTITLE_FONT_OPTIONS:
        req = RecapConfigRequest(url="https://example.com/video.mp4", sub_font=key)
        assert req.sub_font == key


def test_recap_config_request_sub_font_aliases_and_fallback():
    """Full family names or unknown strings resolve safely via resolve_subtitle_font."""
    req1 = RecapConfigRequest(url="https://example.com/video.mp4", sub_font="Noto Serif Myanmar")
    assert req1.sub_font == "serif"

    req2 = RecapConfigRequest(url="https://example.com/video.mp4", sub_font="Padauk")
    assert req2.sub_font == "padauk"

    # Unknown font safely falls back to 'default'
    req3 = RecapConfigRequest(url="https://example.com/video.mp4", sub_font="ComicSansUnknown")
    assert req3.sub_font == "default"


# ── 2. Downloader & Pipeline Signature Verification ───────────────────────


def test_downloader_generate_recap_video_accepts_sub_font():
    """MediaDownloader.generate_recap_video accepts sub_font with default 'default'."""
    sig = inspect.signature(MediaDownloader.generate_recap_video)
    assert "sub_font" in sig.parameters
    assert sig.parameters["sub_font"].default == "default"


def test_downloader_generate_recap_preview_accepts_sub_font():
    """MediaDownloader.generate_recap_preview accepts sub_font with default 'default'."""
    sig = inspect.signature(MediaDownloader.generate_recap_preview)
    assert "sub_font" in sig.parameters
    assert sig.parameters["sub_font"].default == "default"


# ── 3. Mini App HTML Structure & Liquid Glass Classes ─────────────────────


def test_miniapp_html_contains_sub_font_picker():
    """The Recap settings area contains #recap-sub-font-picker with 3 options."""
    html_file = REPO_ROOT / "app" / "webapp" / "static" / "index.html"
    assert html_file.is_file()
    html_content = html_file.read_text(encoding="utf-8")

    assert 'id="recap-sub-font-picker"' in html_content
    assert 'data-i18n="recap_sub_font_label"' in html_content

    # Three options matching backend options
    assert 'data-font="default"' in html_content
    assert 'data-font="serif"' in html_content
    assert 'data-font="padauk"' in html_content

    # Default option is active
    match = re.search(r'<button[^>]*data-font="default"[^>]*>', html_content)
    assert match is not None
    assert "active" in match.group(0)

    # Uses Liquid Glass pill-group structure
    picker_block = re.search(
        r'<div[^>]*id="recap-sub-font-picker"[^>]*>(.*?)</div>',
        html_content,
        re.DOTALL,
    )
    assert picker_block is not None
    assert "pill-group" in picker_block.group(0)
    assert "pill-btn" in picker_block.group(0)


# ── 4. i18n Translation Key Parity ─────────────────────────────────────────


def _extract_i18n_keys(locale_block: str) -> dict[str, str]:
    pattern = r'([a-zA-Z0-9_]+)\s*:\s*["\']([^"\']+)["\']'
    return dict(re.findall(pattern, locale_block))


def test_i18n_key_parity_for_subtitle_font():
    """All 4 subtitle font i18n keys exist with non-empty translations in both my and en."""
    i18n_file = REPO_ROOT / "app" / "webapp" / "static" / "js" / "i18n.js"
    assert i18n_file.is_file()
    i18n_content = i18n_file.read_text(encoding="utf-8")

    expected_keys = {
        "recap_sub_font_label",
        "recap_sub_font_default",
        "recap_sub_font_serif",
        "recap_sub_font_padauk",
    }

    # Extract my section
    my_match = re.search(r'my\s*:\s*{(.*?)}\s*,\s*en\s*:', i18n_content, re.DOTALL)
    assert my_match is not None, "Could not find 'my' locale section"
    my_keys = _extract_i18n_keys(my_match.group(1))

    # Extract en section
    en_match = re.search(r'en\s*:\s*{(.*?)}\s*};', i18n_content, re.DOTALL)
    assert en_match is not None, "Could not find 'en' locale section"
    en_keys = _extract_i18n_keys(en_match.group(1))

    for key in expected_keys:
        assert key in my_keys, f"Missing key '{key}' in 'my' locale"
        assert len(my_keys[key].strip()) > 0, f"Empty key '{key}' in 'my' locale"
        assert key in en_keys, f"Missing key '{key}' in 'en' locale"
        assert len(en_keys[key].strip()) > 0, f"Empty key '{key}' in 'en' locale"


# ── 5. app.js State and Payload Integration ───────────────────────────────


def test_app_js_state_and_payload():
    """app.js registers state.recapSubFont and includes it in start recap request."""
    app_js = (REPO_ROOT / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert 'recapSubFont: "default"' in app_js
    assert 'sub_font: state.recapSubFont || "default"' in app_js
    assert 'recap-sub-font-picker' in app_js
    assert 'data-font' in app_js


# ── 6. Phone Width & Overflow Audit ───────────────────────────────────────


def test_phone_width_overflow_audit():
    """Verify that subtitle font button labels comfortably fit narrow phone viewports.

    Tests viewports from 360px (compact Android / iPhone SE) to 480px.
    With container padding (~32px total), pill-group padding (~6px),
    and per-pill padding (26px per button), text width must leave ample headroom.
    """
    i18n_file = REPO_ROOT / "app" / "webapp" / "static" / "js" / "i18n.js"
    i18n_content = i18n_file.read_text(encoding="utf-8")

    my_match = re.search(r'my\s*:\s*{(.*?)}\s*,\s*en\s*:', i18n_content, re.DOTALL)
    en_match = re.search(r'en\s*:\s*{(.*?)}\s*};', i18n_content, re.DOTALL)

    my_dict = _extract_i18n_keys(my_match.group(1))
    en_dict = _extract_i18n_keys(en_match.group(1))

    # Font pill keys
    pill_keys = ["recap_sub_font_default", "recap_sub_font_serif", "recap_sub_font_padauk"]

    for lang_code, lang_dict in [("my", my_dict), ("en", en_dict)]:
        labels = [lang_dict[k] for k in pill_keys]
        # Approximate width: each character at 13.5px font-size is roughly ~8-9px wide
        # Plus 26px padding (13px left + 13px right) per button
        # Plus 4px gap between buttons
        est_pill_widths = [len(label) * 9 + 26 for label in labels]
        total_pills_width = sum(est_pill_widths) + (len(labels) - 1) * 4

        # For phone widths (360px screen with 32px card margin -> 328px available width)
        available_phone_width = 360 - 32
        assert total_pills_width < available_phone_width, (
            f"Labels in {lang_code} ({labels}) estimated at {total_pills_width}px "
            f"exceeds phone width {available_phone_width}px"
        )
